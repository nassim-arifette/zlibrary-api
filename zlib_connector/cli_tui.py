"""Optional terminal interface, connected only to an existing local API."""
import os
from pathlib import Path
import sys

from .local_state import state_directory


# The TUI's starting pages, spelled out so that --help never imports Textual.
VIEWS = ("home", "favorites", "booklists", "history", "search", "popular", "recommended")


def add_tui_parser(commands):
    command = commands.add_parser("tui", help="browse collections, search and discover books in an interactive terminal")
    command.add_argument("--api-url", default="http://127.0.0.1:8000/v1", help="running loopback HTTP API address")
    command.add_argument("--token-file", type=Path, help="existing server token file (default: state directory/api-token)")
    command.add_argument("--view", choices=VIEWS, default="home",
                         help="starting page; home reads nothing and search opens the search form (default: home)")
    command.add_argument("--page-size", type=int, default=20, help="favorites, history and search page size, 1-50 (default: 20)")
    command.add_argument("--no-color", action="store_true", help="use monochrome output; also honors NO_COLOR")


def run_tui(args):
    if type(args.page_size) is not int or not 1 <= args.page_size <= 50:
        raise ValueError("Choose a TUI page size between 1 and 50.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("The TUI needs an interactive terminal. Use the CLI commands with --json in scripts.")
    try:
        from .tui.app import CollectionsApp
    except ModuleNotFoundError as error:
        if error.name == "textual" or (error.name or "").startswith("textual."):
            raise ValueError('Install the optional terminal interface from the project folder: python -m pip install ".[tui]".') from None
        raise
    from .catalog_api import CatalogAPI

    try:
        api = CatalogAPI(args.api_url, args.token_file or state_directory() / "api-token", timeout=args.timeout)
    except OSError:
        raise ValueError("Could not read the local API token. Start zlib-cli serve or select its existing token file with --token-file.") from None
    # Textual reads NO_COLOR when constructing App. Apply the per-launch
    # option without changing the caller's environment after construction.
    saved_no_color = os.environ.get("NO_COLOR")
    try:
        if args.no_color:
            os.environ["NO_COLOR"] = "1"
        app = CollectionsApp(api, initial_view=args.view, page_size=args.page_size)
    finally:
        if saved_no_color is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = saved_no_color
    app.run()
