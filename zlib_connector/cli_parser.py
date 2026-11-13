"""Command grammar shared by the CLI entry point and interactive shell.

Global options are defined once (``common_options``) with ``default=SUPPRESS`` and
added to the root and to every command and action parser, so they work before
or after the command. ``finish`` then fills the defaults and refuses conflicts.
"""
import argparse
import difflib
import re
from pathlib import Path

from .client import SUPPORTED_FORMATS
from .library import FAVORITE_ORDERS, HISTORY_ORDERS
from .search import SEARCH_ORDERS
from .http_cli import add_serve_parser
from .mcp_cli import add_mcp_parser
from .cli_session import add_session_options
from .cli_jobs import add_jobs_parser
from .cli_tui import add_tui_parser
from .cli_batch import add_batch_options
from .cli_output import UsageError, exit_codes_text
from .cli_config import add_config_parser
from .cli_examples import COMMANDS, HIDDEN, examples_text, groups_text

LIBRARY_COMMANDS = ("library", "favorites", "history", "booklists", "tags")
DISCOVERY_COMMANDS = ("similar", "popular", "recommended")
CATALOG_COMMANDS = ("search", "info", "formats", *DISCOVERY_COMMANDS)
BOOKLIST_MUTATIONS = ("add", "remove", "create", "edit", "delete")
# --timeout defaults to None so that an explicit value can be told apart; the settings fill it (default 30).
GLOBAL_DEFAULTS = {"domain": None, "credentials": None, "timeout": None, "session_file": None, "no_session": False,
                   "json": False, "no_input": False, "interactive": False, "config": None, "no_config": False,
                   "quiet": False, "progress": None}
_CONFLICTS = (("no_input", "--no-input", "interactive", "--interactive"),
              ("session_file", "--session-file", "no_session", "--no-session"),
              ("config", "--config", "no_config", "--no-config"))


class CLIParser(argparse.ArgumentParser):
    """Usage errors become UsageError, so main() reports them like every other failure."""

    finishes = False  # Only the root parser fills global defaults; subparsers must not overwrite them.

    def error(self, message):
        if self.finishes:
            if "required: COMMAND" in message:
                error = UsageError("no command given")
                error.help_text = self.format_help()
                raise error
            typed = re.search(r"invalid choice: '([^']*)'", message)
            if typed is not None and "COMMAND" in message.split(":", 1)[0]:
                close = difflib.get_close_matches(typed.group(1), [name for name in COMMANDS if name not in HIDDEN], n=1)
                suggestion = f" Did you mean: {close[0]}?" if close else " Run: zlib-cli --help"
                raise UsageError("unknown command." + suggestion)
        raise UsageError(message)

    def parse_known_args(self, args=None, namespace=None):
        result, extras = super().parse_known_args(args, namespace)
        if self.finishes:
            finish(result)
        return result, extras


def package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("zlib-connector")
        except PackageNotFoundError:
            return "unknown"
    except ImportError:  # pragma: no cover - importlib.metadata is in the standard library
        return "unknown"


def _common_arguments(target) -> None:
    suppress = argparse.SUPPRESS
    target.add_argument("--domain", default=suppress, help="override the configured site HTTPS origin")
    target.add_argument("--credentials", type=Path, default=suppress,
                        help="local TOML file (default: credentials.toml in the config folder, see config path)")
    target.add_argument("--timeout", type=float, default=suppress,
                        help="request deadline in seconds (default: the timeout setting, else 30)")
    target.add_argument("--config", type=Path, default=suppress, metavar="FILE",
                        help="settings file for this run (default: ZLIB_CONFIG, else config path)")
    target.add_argument("--no-config", action="store_true", default=suppress,
                        help="ignore the settings file for this run; ZLIB_* variables still apply")
    target.add_argument("--session-file", type=Path, default=suppress,
                        help="private saved session (default: state directory/session.json)")
    target.add_argument("--no-session", action="store_true", default=suppress,
                        help="do not read or save persistent sessions")
    target.add_argument("--json", action="store_true", default=suppress,
                        help="JSON on stdout; errors become one JSON object on stderr (docs/cli-json.md)")
    target.add_argument("--no-input", action="store_true", default=suppress,
                        help="never prompt; same as ZLIB_NO_INPUT=1")
    target.add_argument("--interactive", action="store_true", default=suppress,
                        help="allow prompts without a terminal, reading answers from stdin; with download, a list run")
    target.add_argument("--quiet", action="store_true", default=suppress,
                        help="hide notes and progress on stderr; errors and data are kept")
    progress = target.add_mutually_exclusive_group() if hasattr(target, "add_mutually_exclusive_group") else target
    progress.add_argument("--progress", dest="progress", action="store_true", default=suppress,
                          help="show progress on stderr even without a terminal (default: the progress setting, else auto)")
    progress.add_argument("--no-progress", dest="progress", action="store_false", default=suppress,
                          help="hide progress")


def common_options() -> argparse.ArgumentParser:
    """The global options as a parent parser."""
    options = argparse.ArgumentParser(add_help=False)
    _common_arguments(options)
    return options


def _subparsers(target):
    for action in target._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield from action.choices.values()


def _add_common_everywhere(target) -> None:
    for child in _subparsers(target):
        if "--json" not in child._option_string_actions:
            group = child.add_argument_group("global options")
            _common_arguments(group)
        _add_common_everywhere(child)


def command_names() -> tuple:
    """The command names, for messages that must never echo arbitrary text."""
    for action in parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return tuple(action.choices)
    return ()


def finish(args):
    """Fill global defaults after parsing and refuse conflicts given at any position."""
    for name, value in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    for first, first_flag, second, second_flag in _CONFLICTS:
        if getattr(args, first) and getattr(args, second):
            raise UsageError(f"argument {second_flag}: not allowed with argument {first_flag}")
    return args


def _change_options(command, *, lists=False) -> None:
    """--yes and --dry-run for the nine account changes; -a FILE where a list of references is accepted."""
    if lists:
        command.add_argument("-a", "--batch-file", metavar="FILE",
                             help="read references from a TXT, CSV or JSON file, such as the --json output of"
                                  " search, favorites or booklists show; a list change needs --yes")
    command.add_argument("--yes", action="store_true", help="apply the account change without prompting")
    command.add_argument("--dry-run", action="store_true",
                         help="describe the change without sending it; reads are allowed, nothing is asked")


def parser() -> argparse.ArgumentParser:
    result = CLIParser(prog="zlib-cli", description="Z-Library CLI for search, downloads and personal collections.",
                       epilog=exit_codes_text(), formatter_class=argparse.RawDescriptionHelpFormatter)
    result.finishes = True
    _common_arguments(result)
    result.add_argument("--version", action="version", version=f"zlib-cli {package_version()}")
    commands = result.add_subparsers(dest="command", required=True, metavar="COMMAND",
                                     help="run zlib-cli COMMAND --help for its options and examples")
    add_session_options(result, commands)
    add_jobs_parser(commands)
    add_tui_parser(commands)
    for name in ("search", "download"):
        command = commands.add_parser(name)
        if name == "search":
            command.add_argument("query", help="title, author or ISBN")
        else:
            command.add_argument("query", nargs="*", help="titles, authors, ISBNs, ID:HASH references or book URLs;"
                                 " one value without list options downloads a single book")
        command.add_argument("--format", choices=SUPPORTED_FORMATS, action="append", help="format filter; repeat for multiple formats")
        command.add_argument("--language", action="append", help="language filter, e.g. french; repeat for multiple languages")
        command.add_argument("--year-from", type=int, help="earliest publication year")
        command.add_argument("--year-to", type=int, help="latest publication year")
        command.add_argument("--order", choices=SEARCH_ORDERS, help="result order: bestmatch (default), popular, date (recently added), titleA (A to Z),"
                             " title (Z to A), year, filesize (largest first), filesizeA (smallest first)")
        command.add_argument("--page", type=int, default=1 if name == "search" else None, help="result page, from 1 (default: 1)")
        command.add_argument("--limit", type=int, default=20 if name == "search" else None,
                             help="results per page, 1-50 (default: 20)" if name == "search"
                             else "results per search (default: 20; 5 per item for a list)")
        if name == "search":
            command.add_argument("--with-pagination", action="store_true", help="with --json, return books and pagination metadata")
            command.add_argument("--anonymous", action="store_true", help="search without loading or sending credentials")
        if name == "download":
            command.add_argument("--pick", type=int, help="1-based result number for every search; otherwise prompt")
            command.add_argument("--output", type=Path, help="existing destination directory (default: the"
                                 " download_dir setting, else the current directory)")
            add_batch_options(command)
    for name in ("info", "formats"):
        command = commands.add_parser(name)
        command.add_argument("reference", help="ID:HASH or a book HTTPS URL on the selected domain")
        command.add_argument("--anonymous", action="store_true", help="do not load or send credentials")
    for name in ("profile", "quota"):
        commands.add_parser(name)
    for name in DISCOVERY_COMMANDS:
        command = commands.add_parser(name, help=f"browse {name} books")
        if name == "similar":
            command.add_argument("reference", help="ID:HASH or a book URL on the selected domain")
        if name != "recommended":
            command.add_argument("--anonymous", action="store_true", help="do not load or send credentials")
        command.add_argument("--limit", type=int, default=20, help="maximum books shown from the server's list (default: 20)")
    for name, orders in (("favorites", FAVORITE_ORDERS), ("history", HISTORY_ORDERS)):
        command = commands.add_parser(name, help=f"browse account {name}")
        command.add_argument("--page", type=int, default=1, help="page, from 1 (default: 1)")
        command.add_argument("--limit", type=int, default=20, help="books per page (default: 20)")
        command.add_argument("--order", choices=orders, default="saved_date" if name == "favorites" else None,
                             help="sort order" + (" (default: saved_date)" if name == "favorites" else ""))
        if name == "favorites":
            actions = command.add_subparsers(dest="favorite_action")
            for action in ("add", "remove"):
                change = actions.add_parser(action, help=f"{action} books in your account favorites")
                change.add_argument("reference", nargs="?", help="ID:HASH, a book URL, or - to read a list of references from standard input")
                _change_options(change, lists=True)
    tags = commands.add_parser("tags", help="native My Library tags and saved book IDs")
    tags.set_defaults(tag_action="list")
    tag_actions = tags.add_subparsers(dest="tag_action")
    for action in ("list", "saved-ids", "add", "remove"):
        command = tag_actions.add_parser(action)
        if action in ("add", "remove"):
            command.add_argument("reference", nargs="?", help="ID:HASH, a book URL, or - to read a list of references from standard input")
            command.add_argument("tags", nargs="+", help="exact tag labels; quote labels containing spaces")
            _change_options(command, lists=True)
    library = commands.add_parser("library", help="overview of favorites, download history and booklists")
    library.add_argument("--limit", type=int, default=5, help="preview size per book collection")
    booklists = commands.add_parser("booklists", help="browse the booklists on your account")
    booklists.set_defaults(booklist_action="list", page=1)
    actions = booklists.add_subparsers(dest="booklist_action")
    list_command = actions.add_parser("list", help="list account booklists")
    show_command = actions.add_parser("show", help="read a booklist's books")
    show_command.add_argument("booklist_id", help="booklist ID from booklists list")
    show_command.add_argument("--page", type=int, default=1, help="page, from 1 (default: 1)")
    for action in ("add", "remove"):
        change = actions.add_parser(action, help=f"{action} a book in an existing booklist")
        change.add_argument("booklist_id", help="booklist ID from booklists list")
        if action == "add":
            change.add_argument("reference", nargs="?", help="ID:HASH, a book URL, or - to read a list of references from standard input")
        else:
            change.add_argument("entry_id", help="entry ID from booklists show, distinct from the book ID")
        _change_options(change, lists=action == "add")
    for action in ("create", "edit", "delete"):
        change = actions.add_parser(action, help=f"{action} a remote booklist")
        if action == "create":
            change.add_argument("title", help="booklist name")
        else:
            change.add_argument("booklist_id", help="booklist ID from booklists list")
        if action != "delete":
            if action == "edit":
                change.add_argument("--title", help="new name; other settings are preserved")
            change.add_argument("--description", default="" if action == "create" else None,
                                help="description, up to 1000 characters; use an empty string to clear it")
            change.add_argument("--color", type=int, choices=range(6), default=0 if action == "create" else None,
                                help="color number 0-5" + (" (default: 0)" if action == "create" else ""))
            visibility = change.add_mutually_exclusive_group()
            visibility.add_argument("--public", dest="is_public", action="store_true", help="make the list public")
            visibility.add_argument("--private", dest="is_public", action="store_false", help="make the list private (default for new lists)")
            change.set_defaults(is_public=False if action == "create" else None)
        _change_options(change)
    commands.add_parser("auth-test", help="test login using local credentials, then discard the session")
    commands.add_parser("shell", help="interactive session: log in once, search and download repeatedly")
    commands.add_parser("check", help="check public HTTPS access without credentials")
    doctor = commands.add_parser("doctor", help="diagnose real anonymous search on known or specified domains")
    doctor.add_argument("domains", nargs="*", help="HTTPS origins; defaults to --domain or the known candidates")
    doctor.add_argument("--workers", type=int, default=4, help="concurrent isolated probes, 1 to 8 (default: 4)")
    commands.add_parser("domains", help="list known candidates without probing them")
    add_serve_parser(commands)
    add_mcp_parser(commands)
    add_config_parser(commands)
    _add_common_everywhere(result)
    _describe(result, commands)
    return result


_METAVARS = {"reference": "ID:HASH", "booklist_id": "BOOKLIST_ID", "entry_id": "ENTRY_ID", "task_id": "TASK_ID"}


def _describe(root, commands) -> None:
    """Summaries, descriptions, examples and metavars from cli_examples; hidden commands leave the root help."""
    from .settings import config_file_path
    try:
        location = str(config_file_path()[0])
    except (OSError, RuntimeError, ValueError):
        location = "see zlib-cli config path"
    helps = {action.dest: action.help for action in commands._choices_actions}
    summaries = {name: helps.get(name) or COMMANDS.get(name, ("",))[0] for name in commands.choices if name not in HIDDEN}
    root.description = ("Z-Library CLI for search, downloads and personal collections.\n\n"
                        + groups_text(summaries))
    root.epilog = (exit_codes_text() + f"\nsettings file: {location}\nScripts should use --json; text output may change.")
    commands._choices_actions[:] = []
    for name, command in commands.choices.items():
        summary, description, _ = COMMANDS.get(name, (None, None, ()))
        if summary is not None:
            command.description = command.description or f"{summary[0].upper()}{summary[1:]}. {description}"
            command.epilog = examples_text(name) + (f"\n\n{command.epilog}" if command.epilog else "")
            command.formatter_class = argparse.RawDescriptionHelpFormatter
        _metavars(command)


def _metavars(command) -> None:
    for action in command._actions:
        if action.dest in _METAVARS and not action.option_strings and action.metavar is None:
            action.metavar = _METAVARS[action.dest]
        if action.dest == "order" and action.option_strings and action.choices and not action.help:
            action.help = "sort order: " + ", ".join(action.choices) + " (docs/reference.md describes each)"
    for child in _subparsers(command):
        _metavars(child)
