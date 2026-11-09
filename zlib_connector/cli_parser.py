"""Command grammar shared by the CLI entry point and interactive shell."""
import argparse
from pathlib import Path

from .client import SUPPORTED_FORMATS
from .library import FAVORITE_ORDERS, HISTORY_ORDERS
from .search import SEARCH_ORDERS

LIBRARY_COMMANDS = ("library", "favorites", "history", "booklists")
DISCOVERY_COMMANDS = ("similar", "popular", "recommended")
CATALOG_COMMANDS = ("search", "info", "formats", *DISCOVERY_COMMANDS)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="zlib-cli", description="Z-Library CLI for search, downloads and personal collections. Sessions remain in memory.")
    result.add_argument("--domain", default=None, help="override the configured site HTTPS origin")
    result.add_argument("--credentials", type=Path, help="local TOML file (default: credentials.local.toml beside the package)")
    result.add_argument("--timeout", type=float, default=30, help="request deadline in seconds (default: 30)")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("search", "download"):
        command = commands.add_parser(name)
        command.add_argument("query", help="title, author or ISBN; download also accepts ID:HASH or a book URL")
        command.add_argument("--format", choices=SUPPORTED_FORMATS, action="append", help="format filter; repeat for multiple formats")
        command.add_argument("--language", action="append", help="language filter, e.g. french; repeat for multiple languages")
        command.add_argument("--year-from", type=int)
        command.add_argument("--year-to", type=int)
        command.add_argument("--order", choices=SEARCH_ORDERS)
        command.add_argument("--page", type=int, default=1)
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--json", action="store_true", help="machine-readable stdout; prompts stay on stderr")
        if name == "search":
            command.add_argument("--with-pagination", action="store_true", help="with --json, return books and pagination metadata")
            command.add_argument("--anonymous", action="store_true", help="search without loading or sending credentials")
        if name == "download":
            command.add_argument("--pick", type=int, help="1-based result number; otherwise prompt")
            command.add_argument("--output", type=Path, default=Path.cwd(), help="existing destination directory")
    for name in ("info", "formats"):
        command = commands.add_parser(name)
        command.add_argument("reference", help="ID:HASH or a book HTTPS URL on the selected domain")
        command.add_argument("--anonymous", action="store_true", help="do not load or send credentials")
        command.add_argument("--json", action="store_true")
    for name in ("profile", "quota"):
        commands.add_parser(name).add_argument("--json", action="store_true")
    for name in DISCOVERY_COMMANDS:
        command = commands.add_parser(name, help=f"browse {name} books")
        if name == "similar":
            command.add_argument("reference", help="ID:HASH or a book URL on the selected domain")
        if name != "recommended":
            command.add_argument("--anonymous", action="store_true", help="do not load or send credentials")
        command.add_argument("--limit", type=int, default=20, help="maximum books shown from the server's list (default: 20)")
        command.add_argument("--json", action="store_true")
    for name, orders in (("favorites", FAVORITE_ORDERS), ("history", HISTORY_ORDERS)):
        command = commands.add_parser(name, help=f"browse account {name}")
        command.add_argument("--page", type=int, default=1)
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--order", choices=orders, default="saved_date" if name == "favorites" else None)
        command.add_argument("--json", action="store_true")
        if name == "favorites":
            actions = command.add_subparsers(dest="favorite_action")
            for action in ("add", "remove"):
                change = actions.add_parser(action, help=f"{action} a book in your account favorites")
                change.add_argument("reference", help="ID:HASH or a book URL on the selected domain")
                change.add_argument("--yes", action="store_true", help="apply the account change without prompting")
                change.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    library = commands.add_parser("library", help="overview of favorites, download history and booklists")
    library.add_argument("--limit", type=int, default=5, help="preview size per book collection")
    library.add_argument("--json", action="store_true")
    booklists = commands.add_parser("booklists", help="browse the booklists on your account")
    booklists.add_argument("--json", action="store_true")
    booklists.set_defaults(booklist_action="list", page=1)
    actions = booklists.add_subparsers(dest="booklist_action")
    list_command = actions.add_parser("list", help="list account booklists")
    list_command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    show_command = actions.add_parser("show", help="read a booklist's books")
    show_command.add_argument("booklist_id")
    show_command.add_argument("--page", type=int, default=1)
    show_command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    for action in ("add", "remove"):
        change = actions.add_parser(action, help=f"{action} a book in an existing booklist")
        change.add_argument("booklist_id")
        if action == "add":
            change.add_argument("reference", help="ID:HASH or a book URL on the selected domain")
        else:
            change.add_argument("entry_id", help="entry ID from booklists show, distinct from the book ID")
        change.add_argument("--yes", action="store_true", help="apply the account change without prompting")
        change.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    commands.add_parser("auth-test", help="test login using local credentials, then discard the session")
    commands.add_parser("shell", help="interactive session: log in once, search and download repeatedly")
    commands.add_parser("check", help="check public HTTPS access without credentials")
    doctor = commands.add_parser("doctor", help="diagnose real anonymous search on known or specified domains")
    doctor.add_argument("domains", nargs="*", help="HTTPS origins; defaults to --domain or the known candidates")
    doctor.add_argument("--workers", type=int, default=4, help="concurrent isolated probes, 1–8 (default: 4)")
    doctor.add_argument("--json", action="store_true")
    commands.add_parser("domains", help="list known candidates without probing them").add_argument("--json", action="store_true")
    return result
