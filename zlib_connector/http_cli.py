"""CLI lifecycle for the local HTTP API; bearer and account secrets stay local."""

import getpass
import ipaddress
from pathlib import Path
import sys

from .credentials import load_credentials
from .eapi import Client
from .http_api import create_server
from .http_api.server import _allowed_origin
from .api_token import load_api_token
from .jobs.models import JobBusyError
from .local_state import state_directory
from .cli_session import AccountSession
from .cli_terminal import InputPolicy
from .cli_output import UsageError, print_json
from .cli_settings import configured_folder, ensure_settings
from .local_api import _address


class _DefaultHost(str):
    """The --host default; tells a default apart from an explicit 127.0.0.1."""


class _DefaultPort(int):
    """The --port default; tells a default apart from an explicit 8000."""


DEFAULT_HOST, DEFAULT_PORT = _DefaultHost("127.0.0.1"), _DefaultPort(8000)


def add_serve_parser(commands):
    command = commands.add_parser("serve", help="serve the authenticated local HTTP API")
    command.add_argument("--host", default=DEFAULT_HOST,
                         help="IPv4 loopback address or localhost (default: the api_url setting's host, else 127.0.0.1)")
    command.add_argument("--port", type=int, default=DEFAULT_PORT,
                         help="local HTTP port (default: the api_url setting's port, else 8000; 0 selects an available"
                              " port)")
    command.add_argument("--token-file", type=Path,
                         help="local bearer file; created if absent (default: state directory/api-token)")
    command.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                         help="allow an explicit browser origin; repeat for several (default: none)")
    command.add_argument("--anonymous", action="store_true", help="serve without loading or sending account credentials")
    folders = command.add_mutually_exclusive_group()
    folders.add_argument("--download-dir", type=Path,
                         help="keep download tasks in this existing directory (default: state directory/downloads)")
    folders.add_argument("--downloads", action="store_true",
                         help="keep download tasks in the download_dir setting's folder")
    folders.add_argument("--no-downloads", action="store_true",
                         help="turn download tasks off; they are on by default for a signed-in server")
    command.add_argument("--max-download-tasks", type=int, default=1000,
                         help="maximum retained tasks per account (1-10000; default: 1000)")
    return command


def _prompt(label):
    print(label, end="", file=sys.stderr, flush=True)
    value = sys.stdin.readline()
    if not value:
        raise EOFError
    return value.strip()


def _password_prompt(label):
    return getpass.getpass(label, stream=sys.stderr)


def _validate_listener(args):
    """Reject local configuration mistakes before reading any credential file."""
    host = "127.0.0.1" if args.host == "localhost" else args.host
    try:
        address = ipaddress.IPv4Address(host)
    except (ValueError, TypeError):
        raise ValueError("Bind the API to localhost or an IPv4 loopback address.") from None
    if not address.is_loopback:
        raise ValueError("The HTTP API currently supports loopback addresses only.")
    if type(args.port) is not int or not 0 <= args.port <= 65535:
        raise ValueError("The API port must be between 0 and 65535.")
    if type(args.timeout) not in (int, float) or not 0 < args.timeout <= 300:
        raise ValueError("The upstream timeout must be between 0 and 300 seconds.")
    if len(args.allow_origin) > 20:
        raise ValueError("At most 20 browser origins can be allowed.")
    for origin in args.allow_origin:
        _allowed_origin(origin)
    download_dir = getattr(args, "download_dir", None)
    capacity = getattr(args, "max_download_tasks", 1000)
    if type(capacity) is not int or not 1 <= capacity <= 10000:
        raise ValueError("Choose a task capacity between 1 and 10000.")
    if download_dir is not None:
        if args.anonymous:
            raise ValueError("Download tasks require an authenticated account.")
        if not download_dir.is_dir() or download_dir.is_symlink():
            raise ValueError("Choose an existing download directory, not a link.")


def _listen_address(args, settings):
    """Without --host and --port, listen on a configured api_url (environment or config file)."""
    host_default = isinstance(args.host, _DefaultHost)
    port_default = isinstance(args.port, _DefaultPort)
    if host_default and port_default and settings.origin("api_url") in ("env", "config"):
        args.host, args.port = _address(settings.api_url)
        return
    args.host = str(args.host) if host_default else args.host
    args.port = int(args.port) if port_default else args.port


def default_task_folder() -> Path:
    """Where a signed-in server keeps its download tasks when no folder is chosen."""
    return state_directory() / "downloads"


def _task_folder(args):
    """The task directory to enable, or None: off with --no-downloads or --anonymous."""
    if getattr(args, "download_dir", None) is not None:
        return args.download_dir
    if getattr(args, "no_downloads", False) or args.anonymous:
        return None
    folder = default_task_folder()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def _download_folder(args):
    """--downloads takes the configured folder; without it, the configured folder is never the task directory."""
    if not getattr(args, "downloads", False):
        return
    if getattr(args, "download_dir", None) is not None:
        raise UsageError("argument --downloads: not allowed with argument --download-dir")
    folder = configured_folder(args)
    if folder is None:
        raise ValueError("No download folder is configured. Use --download-dir DIR, or zlib-cli config set"
                         " download_dir DIR.")
    args.download_dir = folder


def run_server(args, *, client_factory=Client, credentials_loader=load_credentials,
               prompt=_prompt, password_prompt=_password_prompt, server_factory=None, policy=None):
    """Bind before prompting/login; close server workers before its Client.

    KeyboardInterrupt deliberately propagates after cleanup so the CLI can
    return its normal interrupted status. No shutdown call is needed when
    serve_forever exits on the current thread.
    """
    server_factory = create_server if server_factory is None else server_factory
    settings = ensure_settings(args)
    _listen_address(args, settings)
    _download_folder(args)
    _validate_listener(args)
    token_path = (args.token_file or state_directory() / "api-token").expanduser().absolute()
    token = load_api_token(token_path)
    account = AccountSession(args, credentials_loader=credentials_loader)
    # Refuse a sign-in prompt that cannot be answered before the port is bound.
    account.require_input(InputPolicy.from_args(args) if policy is None else policy, explicit=False)
    with client_factory(account.domain, timeout=args.timeout) as client:
        with server_factory(client, token=token, host=args.host, port=args.port,
                            allowed_origins=args.allow_origin) as server:
            account.authenticate(client, prompt=prompt, password_prompt=password_prompt)
            tasks, busy = _task_folder(args), False
            if tasks is not None:
                try:
                    server.enable_downloads(tasks, max_tasks=args.max_download_tasks)
                except JobBusyError:
                    # Only a folder the user chose is an error; the default one may belong to another server.
                    if getattr(args, "download_dir", None) is not None:
                        raise
                    tasks, busy = None, True
            address, port = server.server_address[:2]
            if getattr(args, "json", False):
                print_json({"event": "listening", "url": f"http://{address}:{port}/v1", "token_file": str(token_path),
                            "downloads": tasks is not None})
            else:
                print(f"HTTP API: http://{address}:{port}/v1", file=sys.stderr, flush=True)
                print(f"Bearer token file: {token_path}", file=sys.stderr, flush=True)
                reason = ("another server uses the task folder" if busy else "anonymous server" if args.anonymous
                          else "--no-downloads")
                print("Downloads: on" if tasks is not None else f"Downloads: off ({reason})", file=sys.stderr, flush=True)
                print("Press Ctrl+C to stop.", file=sys.stderr, flush=True)
            server.serve_forever()
