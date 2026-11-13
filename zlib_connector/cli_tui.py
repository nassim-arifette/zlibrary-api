"""Optional terminal interface over the local API; starts a private one when none is running."""
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading

from . import cli_terminal
from .cli_output import CommandError, InputRequired
from .cli_settings import configured_folder, ensure_settings
from .local_state import state_directory


# The TUI's starting pages, spelled out so that --help never imports Textual.
VIEWS = ("home", "favorites", "booklists", "history", "search", "popular", "recommended")


def add_tui_parser(commands):
    command = commands.add_parser("tui", help="browse collections, search and discover books in an interactive terminal")
    command.add_argument("--api-url", help="running loopback HTTP API address (default: the api_url setting, else"
                         " http://127.0.0.1:8000/v1); when no server answers there and neither --api-url nor"
                         " --token-file is given, the TUI starts a private server for this session")
    command.add_argument("--token-file", type=Path, help="existing server token file (default: state directory/api-token)")
    command.add_argument("--no-auto-server", action="store_true",
                         help="never start a private server; connect only to a running zlib-cli serve")
    command.add_argument("--view", choices=VIEWS, default="home",
                         help="starting page; home reads nothing and search opens the search form (default: home)")
    command.add_argument("--page-size", type=int, default=20, help="favorites, history and search page size, 1-50 (default: 20)")
    command.add_argument("--no-color", action="store_true", help="use monochrome output; also honors NO_COLOR")


def run_tui(args):
    ensure_settings(args)
    if type(args.page_size) is not int or not 1 <= args.page_size <= 50:
        raise ValueError("Choose a TUI page size between 1 and 50.")
    if not cli_terminal.is_terminal(sys.stdin) or not cli_terminal.is_terminal(sys.stdout):
        raise InputRequired("The TUI needs an interactive terminal. Use the CLI commands with --json in scripts.",
                            name="ValueError")
    try:
        from .tui.app import CollectionsApp
    except ModuleNotFoundError as error:
        if error.name == "textual" or (error.name or "").startswith("textual."):
            raise CommandError('Install the optional terminal interface from the project folder: python -m pip install ".[tui]".',
                               code="missing_dependency", hint='python -m pip install ".[tui]"') from None
        raise
    from .catalog_api import CatalogAPI

    token_file = args.token_file or state_directory() / "api-token"
    download_dir = download_folder(args)
    with _server_for(args, token_file) as url:
        try:
            api = CatalogAPI(url, token_file, timeout=args.timeout)
        except OSError:
            raise CommandError("Could not read the local API token. Start zlib-cli serve or select its existing token"
                               " file with --token-file.", code="local_api_unavailable") from None
        _run_app(args, CollectionsApp, api, download_dir)


def download_folder(args) -> Path:
    """Where the TUI saves books: the download_dir setting, else the Downloads folder, else the current one."""
    folder = configured_folder(args)
    if folder is not None:
        return folder
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.cwd()


def _run_app(args, CollectionsApp, api, download_dir):
    # Textual reads NO_COLOR when constructing App. Apply the per-launch
    # option without changing the caller's environment after construction.
    saved_no_color = os.environ.get("NO_COLOR")
    try:
        if args.no_color:
            os.environ["NO_COLOR"] = "1"
        app = CollectionsApp(api, initial_view=args.view, page_size=args.page_size, download_dir=download_dir)
    finally:
        if saved_no_color is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = saved_no_color
    app.run()


def _listening(url) -> bool:
    """True when something accepts connections at the API address; a bad address is left to the client."""
    from .local_api import _address
    try:
        host, port = _address(url)
    except ValueError:
        return True
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@contextmanager
def _server_for(args, token_file):
    """Yield the API address: the running server's, or a private one that lives as long as the TUI.

    An explicit --api-url or --token-file names an existing server, so it is never replaced.
    """
    if (getattr(args, "no_auto_server", False) or args.token_file is not None
            or args.settings.origin("api_url") == "cli" or _listening(args.api_url)):
        yield args.api_url
        return
    from .api_token import load_api_token
    from .cli_session import AccountSession
    from .cli_settings import client_factory
    from .cli_terminal import InputPolicy
    from .credentials import load_credentials
    from .eapi import Client
    from .http_api import create_server
    from .http_cli import _password_prompt, _prompt

    token = load_api_token(token_file)
    account = AccountSession(args, credentials_loader=load_credentials)
    account.require_input(InputPolicy.from_args(args), explicit=False)
    print("Starting a private local server for this session...", file=sys.stderr, flush=True)
    # The private server's download tasks live only for this session; each finished book is copied
    # into the download folder, then the task directory goes away with the server.
    with tempfile.TemporaryDirectory(prefix="zlib-tui-", ignore_cleanup_errors=True) as tasks, \
            client_factory(args, Client)(account.domain, timeout=args.timeout) as client, \
            create_server(client, token=token, host="127.0.0.1", port=0) as server:
        account.authenticate(client, prompt=_prompt, password_prompt=_password_prompt)
        server.enable_downloads(Path(tasks))
        worker = threading.Thread(target=server.serve_forever, name="tui-local-api", daemon=True)
        worker.start()
        try:
            address, port = server.server_address[:2]
            yield f"http://{address}:{port}/v1"
        finally:
            server.shutdown()
            worker.join(5)
