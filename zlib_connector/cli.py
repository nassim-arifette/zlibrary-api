"""CLI adapter. Credentials are prompted, never accepted as command-line flags."""
import argparse
import getpass
import json
from pathlib import Path
import re
import shlex
import sys

from .client import SUPPORTED_FORMATS
from .eapi import Client
from .credentials import load_credentials
from .errors import ConnectorError
from .search import SEARCH_ORDERS, SearchFilters, validate_search
from .books import BookReference
from .diagnostics import KNOWN_DOMAINS, diagnose_domains

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="zlib-cli", description="Independent Z-Library search/download client. Sessions remain in memory.")
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
    commands.add_parser("auth-test", help="test login using local credentials, then discard the session")
    commands.add_parser("shell", help="interactive session: log in once, search and download repeatedly")
    commands.add_parser("check", help="check public HTTPS access without credentials")
    doctor = commands.add_parser("doctor", help="diagnose real anonymous search on known or specified domains")
    doctor.add_argument("domains", nargs="*", help="HTTPS origins; defaults to --domain or the known candidates")
    doctor.add_argument("--workers", type=int, default=4, help="concurrent isolated probes, 1–8 (default: 4)")
    doctor.add_argument("--json", action="store_true")
    commands.add_parser("domains", help="list known candidates without probing them").add_argument("--json", action="store_true")
    return result

def prompt(label: str) -> str:
    print(label, end="", file=sys.stderr, flush=True)
    value = sys.stdin.readline()
    if not value:
        raise EOFError
    return value.strip()

def show_books(books, *, file=None):
    file = sys.stdout if file is None else file
    if not books:
        print("No supported books found.", file=file)
    for index, book in enumerate(books, 1):
        print(f"{index:2}. {book.title}\n    {book.authors} · {book.year} · {book.language} · {book.extension.upper()} · {book.size}", file=file)

def search_filters(args) -> SearchFilters:
    validate_search(args.query, args.page, args.limit)
    return SearchFilters(languages=args.language or (), formats=args.format or (),
                         year_from=args.year_from, year_to=args.year_to, order=args.order)


def show_search(client: Client, args, filters: SearchFilters):
    options = {"filters": filters, "page": args.page, "limit": args.limit}
    if args.with_pagination:
        result = client.search_page(args.query, **options)
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False))
        else:
            show_books(result.books)
            print(f"Page {result.page} · total: {result.total_items if result.total_items is not None else 'unknown'}")
    else:
        books = client.search(args.query, **options)
        if args.json:
            print(json.dumps([book.to_dict() for book in books], ensure_ascii=False))
        else:
            show_books(books)


def show_book_command(client: Client, args):
    if args.command == "info":
        book = client.get_book(args.reference)
        if args.json:
            print(json.dumps(book.to_dict(), ensure_ascii=False))
        else:
            show_books([book])
            print(f"Reference: {book.book_id}:{book.book_hash}")
            for label, value in (("Publisher", book.publisher), ("Edition", book.edition),
                                 ("Pages", book.pages), ("ISBN-10", book.isbn10), ("ISBN-13", book.isbn13),
                                 ("Categories", ", ".join(book.categories)), ("Cover", book.cover_url),
                                 ("Description", book.description)):
                if value:
                    print(f"{label}: {value}")
    else:
        formats = client.get_formats(args.reference)
        if args.json:
            print(json.dumps([item.to_dict() for item in formats], ensure_ascii=False))
        elif not formats:
            print("The server reported no alternative formats.")
        else:
            for item in formats:
                support = "" if item.to_dict()["download_supported"] else " (download unsupported)"
                print(f"{item.book_id}:{item.book_hash} · {item.extension.upper()} · {item.size}{support}")


def shell(client: Client):
    books = []
    last_search = None
    result = None
    print('Commands: search "query" [filters] | next | prev | download NUMBER [DIRECTORY] | quit')
    while True:
        try:
            args = shlex.split(prompt("zlib> "))
            if not args:
                continue
            if args == ["quit"] or args == ["exit"]:
                return
            if args[0] == "search" and len(args) >= 2:
                # Keep unquoted queries usable; parse options when present.
                search_args = args if any(value.startswith("--") for value in args[1:]) else ["search", " ".join(args[1:])]
                try:
                    options = parser().parse_args(search_args)
                except SystemExit:
                    continue
                filters = search_filters(options)
                result = client.search_page(options.query, filters=filters, page=options.page, limit=options.limit)
                last_search = (options.query, filters, options.limit)
                books = result.books
                show_books(books)
            elif args in (["next"], ["prev"]):
                if last_search is None or result is None:
                    raise ValueError("Run a search first.")
                page = result.next_page if args[0] == "next" else result.previous_page
                if page is None:
                    print("No page available in that direction.")
                    continue
                query, filters, limit = last_search
                result = client.search_page(query, filters=filters, page=page, limit=limit)
                books = result.books
                show_books(books)
            elif args[0] == "download" and len(args) in (2, 3):
                index = int(args[1])
                if not 1 <= index <= len(books):
                    raise ValueError("Choose a number from the most recent search.")
                print(client.download(books[index - 1], args[2] if len(args) == 3 else "."))
            else:
                print('Use: search "query" [filters], next, prev, download NUMBER [DIRECTORY], or quit.', file=sys.stderr)
        except (ConnectorError, ValueError, OSError) as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)

def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "domains":
            print(json.dumps(list(KNOWN_DOMAINS)) if args.json else "\n".join(KNOWN_DOMAINS))
            return 0
        if args.command == "doctor":
            domains = args.domains or ([args.domain] if args.domain else None)
            report = diagnose_domains(domains, timeout=args.timeout, workers=args.workers)
            if args.json:
                print(json.dumps(report.to_dict()))
            else:
                for result in report.results:
                    state = "ok" if result.search_ok else result.error_code
                    print(f"{result.domain}  {state}  {result.elapsed_ms} ms  {result.message}")
                print(f"Suggested domain: {report.recommended_domain or 'none'}")
            return 0 if report.recommended_domain else 1
        if args.command == "check":
            domain = args.domain or "https://z-lib.by/"
            with Client(domain, timeout=args.timeout) as client:
                books = client.search("Pride and Prejudice", limit=1)
                print(json.dumps({"reachable": True, "domain": client.domain, "api": "eapi", "search_ok": True, "results": len(books), "authenticated": False}))
            return 0
        filters = None
        reference = None
        if args.command in ("info", "formats"):
            reference = args.reference
        if args.command in ("search", "download"):
            filters = search_filters(args)
            if args.command == "download" and (not args.output.is_dir() or (args.pick is not None and args.pick < 1)):
                raise ValueError("Choose an existing output directory and a positive --pick number.")
            if args.command == "download" and (re.match(r"^[0-9]+:", args.query) or "://" in args.query):
                reference = args.query
                if args.pick is not None or filters != SearchFilters() or args.page != 1 or args.limit != 20:
                    raise ValueError("Search filters and --pick cannot be used with a direct book reference.")
        if reference is not None:
            BookReference.parse(reference, args.domain)
        if args.command in ("search", "info", "formats") and args.anonymous:
            with Client(args.domain or "https://z-lib.by/", timeout=args.timeout) as client:
                if args.command == "search":
                    show_search(client, args, filters)
                else:
                    show_book_command(client, args)
            return 0
        credentials = load_credentials(args.credentials)
        with Client(args.domain or credentials.domain, timeout=args.timeout) as client:
            if reference is not None:
                BookReference.parse(reference, client.domain)
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
            if args.command == "search":
                show_search(client, args, filters)
                return 0
            if args.command in ("info", "formats"):
                show_book_command(client, args)
                return 0
            if args.command in ("profile", "quota"):
                profile = client.get_profile()
                if args.json:
                    value = profile.to_dict() if args.command == "profile" else profile.quota.to_dict()
                    print(json.dumps(value))
                else:
                    for name, value in profile.quota.to_dict().items():
                        print(f"{name.capitalize()}: {value if value is not None else 'unknown'}")
                return 0
            if reference is not None:
                selected = reference
            else:
                books = client.search(args.query, filters=filters, page=args.page, limit=args.limit)
                if not books:
                    raise ValueError("No supported books found for this query.")
                if args.pick is None:
                    show_books(books, file=sys.stderr)
                    index = int(prompt("Result number: "))
                else:
                    index = args.pick
                if not 1 <= index <= len(books):
                    raise ValueError("Choose a number from the returned results.")
                selected = books[index - 1]
            path = client.download(selected, args.output)
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
