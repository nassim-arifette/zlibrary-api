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
from .local_state import state_directory
from .cli_session import AccountSession


def add_serve_parser(commands):
    command = commands.add_parser("serve", help="serve the authenticated local HTTP API")
    command.add_argument("--host", default="127.0.0.1", help="IPv4 loopback address or localhost (default: 127.0.0.1)")
    command.add_argument("--port", type=int, default=8000, help="local HTTP port (default: 8000; 0 selects an available port)")
    command.add_argument("--token-file", type=Path,
                         help="local bearer file; created if absent (default: state directory/api-token)")
    command.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                         help="allow an explicit browser origin; repeat for several (default: none)")
    command.add_argument("--anonymous", action="store_true", help="serve without loading or sending account credentials")
    command.add_argument("--download-dir", type=Path,
                         help="enable durable download tasks in this existing directory")
    command.add_argument("--max-download-tasks", type=int, default=1000,
                         help="maximum retained tasks per account (1–10000; default: 1000)")
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


def run_server(args, *, client_factory=Client, credentials_loader=load_credentials,
               prompt=_prompt, password_prompt=_password_prompt, server_factory=create_server):
    """Bind before prompting/login; close server workers before its Client.

    KeyboardInterrupt deliberately propagates after cleanup so the CLI can
    return its normal interrupted status. No shutdown call is needed when
    serve_forever exits on the current thread.
    """
    _validate_listener(args)
    token_path = (args.token_file or state_directory() / "api-token").expanduser().absolute()
    token = load_api_token(token_path)
    account = AccountSession(args, credentials_loader=credentials_loader)
    with client_factory(account.domain, timeout=args.timeout) as client:
        with server_factory(client, token=token, host=args.host, port=args.port,
                            allowed_origins=args.allow_origin) as server:
            account.authenticate(client, prompt=prompt, password_prompt=password_prompt)
            if getattr(args, "download_dir", None) is not None:
                server.enable_downloads(args.download_dir, max_tasks=args.max_download_tasks)
            address, port = server.server_address[:2]
            print(f"HTTP API: http://{address}:{port}/v1", file=sys.stderr, flush=True)
            print(f"Bearer token file: {token_path}", file=sys.stderr, flush=True)
            print("Press Ctrl+C to stop.", file=sys.stderr, flush=True)
            server.serve_forever()
