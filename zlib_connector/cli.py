"""CLI adapter. Credentials are prompted, never accepted as command-line flags."""
import argparse
import getpass
import json
from pathlib import Path
import shlex
import sys

from .client import SUPPORTED_FORMATS
from .eapi import Client
from .credentials import load_credentials
from .errors import ConnectorError

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="zlib-cli", description="Independent Z-Library search/download client. Sessions remain in memory.")
    result.add_argument("--domain", default=None, help="override the configured site HTTPS origin")
    result.add_argument("--credentials", type=Path, help="local TOML file (default: credentials.local.toml beside the package)")
    result.add_argument("--timeout", type=float, default=30, help="request deadline in seconds (default: 30)")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("search", "download"):
        command = commands.add_parser(name)
        command.add_argument("query", help="title, author or ISBN")
        command.add_argument("--format", choices=SUPPORTED_FORMATS)
        command.add_argument("--page", type=int, default=1)
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--json", action="store_true", help="machine-readable stdout; prompts stay on stderr")
        if name == "search":
            command.add_argument("--anonymous", action="store_true", help="search without loading or sending credentials")
        if name == "download":
            command.add_argument("--pick", type=int, help="1-based result number; otherwise prompt")
            command.add_argument("--output", type=Path, default=Path.cwd(), help="existing destination directory")
    commands.add_parser("auth-test", help="test login using local credentials, then discard the session")
    commands.add_parser("shell", help="interactive session: log in once, search and download repeatedly")
    commands.add_parser("check", help="check public HTTPS access without credentials")
    return result

def prompt(label: str) -> str:
    print(label, end="", file=sys.stderr, flush=True)
    value = sys.stdin.readline()
    if not value:
        raise EOFError
    return value.strip()

def show_books(books, *, file=sys.stdout):
    if not books:
        print("No supported books found.", file=file)
    for index, book in enumerate(books, 1):
        print(f"{index:2}. {book.title}\n    {book.authors} · {book.year} · {book.language} · {book.extension.upper()} · {book.size}", file=file)

def shell(client: Client):
    books = []
    print('Commands: search "title or author" | download NUMBER [DIRECTORY] | quit')
    while True:
        try:
            args = shlex.split(prompt("zlib> "))
            if not args:
                continue
            if args == ["quit"] or args == ["exit"]:
                return
            if args[0] == "search" and len(args) >= 2:
                books = client.search(" ".join(args[1:]))
                show_books(books)
            elif args[0] == "download" and len(args) in (2, 3):
                index = int(args[1])
                if not 1 <= index <= len(books):
                    raise ValueError("Choose a number from the most recent search.")
                print(client.download(books[index - 1], args[2] if len(args) == 3 else "."))
            else:
                print('Use: search "query", download NUMBER [DIRECTORY], or quit.', file=sys.stderr)
        except (ConnectorError, ValueError, OSError) as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)

def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "check":
            domain = args.domain or "https://z-lib.by/"
            with Client(domain, timeout=args.timeout) as client:
                books = client.search("Pride and Prejudice", limit=1)
                print(json.dumps({"reachable": True, "domain": client.domain, "api": "eapi", "search_ok": True, "results": len(books), "authenticated": False}))
            return 0
        if args.command not in ("shell", "auth-test"):
            if not args.query.strip() or len(args.query.strip()) > 500 or not 1 <= args.page <= 1000 or not 1 <= args.limit <= 50:
                raise ValueError("Use a query of 1–500 characters, page 1–1000 and limit 1–50.")
            if args.command == "download" and (not args.output.is_dir() or (args.pick is not None and args.pick < 1)):
                raise ValueError("Choose an existing output directory and a positive --pick number.")
        if args.command == "search" and args.anonymous:
            with Client(args.domain or "https://z-lib.by/", timeout=args.timeout) as client:
                books = client.search(args.query, extension=args.format, page=args.page, limit=args.limit)
                if args.json:
                    print(json.dumps([book.to_dict() for book in books], ensure_ascii=False))
                else:
                    show_books(books)
            return 0
        credentials = load_credentials(args.credentials)
        with Client(args.domain or credentials.domain, timeout=args.timeout) as client:
            email = credentials.email or prompt("Email: ")
            password = credentials.password or getpass.getpass("Password: ", stream=sys.stderr)
            del credentials
            try:
                client.login(email, password)
            finally:
                password = ""
            if args.command == "auth-test":
                print(json.dumps({"authenticated": True}))
                return 0
            if args.command == "shell":
                shell(client)
                return 0
            books = client.search(args.query, extension=args.format, page=args.page, limit=args.limit)
            if args.command == "search":
                if args.json:
                    print(json.dumps([book.to_dict() for book in books], ensure_ascii=False))
                else:
                    show_books(books)
                return 0
            if not books:
                raise ValueError("No supported books found for this query.")
            if args.pick is None:
                show_books(books, file=sys.stderr)
                index = int(prompt("Result number: "))
            else:
                index = args.pick
            if not 1 <= index <= len(books):
                raise ValueError("Choose a number from the returned results.")
            path = client.download(books[index - 1], args.output)
            print(json.dumps({"path": str(path)}, ensure_ascii=False) if args.json else path)
        return 0
    except (ConnectorError, ValueError) as error:
        if getattr(args, "json", False):
            print(json.dumps({"error": type(error).__name__, "message": str(error)}), file=sys.stderr)
        else:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 130
    except OSError:
        print("Local I/O failed. Check the destination and permissions.", file=sys.stderr)
        return 1
